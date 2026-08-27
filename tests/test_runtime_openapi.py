"""OpenAPI tools: operations become tools, and requests go through gete's client."""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import ProjectBuilder

from gete.connection import Registry
from gete.declaration import RESOLVED_FILE, Agent, load_project, resolve
from gete.errors import DeclarationError, GeteError
from gete.request_context import clear_tool_call
from gete.runtime import build
from gete.runtime.openapi import OpenApiToolset, openapi_toolset
from gete.runtime.reauthorization import ReauthorizationToolset

TOKEN = "rt_0123456789abcdef"

ROOTED_API: dict[str, Any] = {
    "id": "rooted-api",
    "display_name": "Rooted API",
    "hosts": [],
    "token_prefixes": ["rt_"],
    "base_url": "https://acme.example.com",
    "oauth": {
        "authorization_url": "https://acme.example.com/oauth/authorize",
        "token_url": "https://acme.example.com/oauth/token",
        "scopes": {"read": "Read data"},
    },
}

REGISTRY = Registry.from_documents({"rooted-api": ROOTED_API})

SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Example", "version": "1.0"},
    # Untrustworthy on purpose: nothing may ever read the published servers.
    "servers": [{"url": "https://{tenant}.evil.example"}],
    "paths": {
        "/search": {
            "get": {
                "operationId": "ListSearchResults",
                "description": "Vendor text. See [Query syntax](#query-syntax).",
                "parameters": [
                    {
                        "name": "query",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                    },
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
                    "schema": {"type": "string"},
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
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "ticket": {
                                        "type": "object",
                                        "properties": {"status": {"type": "string"}},
                                    }
                                },
                            }
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
}


class Context:
    """The part of ADK's ReadonlyContext / ToolContext the runtime reads."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.user_id = "user"


def teardown_function() -> None:
    clear_tool_call()


def agent_with_spec(tmp_path: Path, document: dict[str, Any] | None = None) -> Agent:
    (tmp_path / "spec.yaml").write_text(
        yaml.safe_dump(document or SPEC, sort_keys=False)
    )
    return Agent(directory=tmp_path, data={"name": "mail-triage"})


def toolset(
    tmp_path: Path,
    *,
    document: dict[str, Any] | None = None,
    operations: list[str] | None = None,
    effect: str = "read",
    params: dict[str, Any] | None = None,
    only: dict[str, list[str]] | None = None,
    describe: dict[str, str] | None = None,
    does_not: str | None = None,
    confirm: bool = False,
    confirm_names: list[str] | None = None,
    denied: list[str] | None = None,
) -> OpenApiToolset:
    spec: dict[str, Any] = {
        "spec": "./spec.yaml",
        "connection": "rooted-api",
        "operations": operations or ["ListSearchResults", "ShowTicket"],
        "effect": effect,
    }
    if params:
        spec["params"] = params
    if only:
        spec["only"] = only
    if describe:
        spec["describe"] = describe
    if does_not:
        spec["does_not"] = does_not
    return openapi_toolset(
        spec,
        agent=agent_with_spec(tmp_path, document),
        authorizations={"rooted-api": "mail-triage-rooted-api"},
        registry=REGISTRY,
        confirm=confirm,
        confirm_names=confirm_names or (),
        denied=denied or (),
    )


def context() -> Context:
    return Context({"mail-triage-rooted-api": TOKEN})


async def test_each_operation_becomes_a_tool_named_by_its_operation_id(
    tmp_path: Path,
) -> None:
    tools = await toolset(tmp_path).get_tools(context())
    assert [tool.name for tool in tools] == ["ListSearchResults", "ShowTicket"]


async def test_nothing_is_offered_without_a_usable_token(tmp_path: Path) -> None:
    built = toolset(tmp_path)
    assert await built.get_tools(Context({})) == []
    assert await built.get_tools(Context({"mail-triage-rooted-api": "ya29.x"})) == []
    # No context at all counts as no token; the Agent Card is built that way.
    assert await built.get_tools(None) == []


async def test_denied_tools_are_not_offered(tmp_path: Path) -> None:
    built = toolset(tmp_path, denied=["ShowTicket"])
    tools = await built.get_tools(context())
    assert [tool.name for tool in tools] == ["ListSearchResults"]


async def test_a_fixed_parameter_is_not_in_the_declaration(tmp_path: Path) -> None:
    """Its value is declared, so there is nothing for the model to say."""
    built = toolset(
        tmp_path,
        params={"ListSearchResults": {"per_page": {"value": 25}}},
    )
    search = (await built.get_tools(context()))[0]
    declared = search._get_declaration().model_dump_json(exclude_none=True)
    assert "query" in declared
    assert "per_page" not in declared


async def test_describe_replaces_the_vendor_text_and_does_not_rides_along(
    tmp_path: Path,
) -> None:
    built = toolset(
        tmp_path,
        describe={"ListSearchResults": "Search tickets."},
        does_not="Does not read other kinds.",
    )
    search, show = await built.get_tools(context())
    assert (
        search.description == "Search tickets.\n\nDoes not: Does not read other kinds."
    )
    assert "Vendor text" not in search.description
    # Without describe, the vendor's text stays, with the same rider.
    assert show.description.endswith("Does not: Does not read other kinds.")


async def test_write_tools_ask_for_confirmation_when_told_to(tmp_path: Path) -> None:
    built = toolset(tmp_path, operations=["UpdateTicket"], effect="write", confirm=True)
    update = (await built.get_tools(context()))[0]
    assert await update.check_require_confirmation({}, context()) is True


async def test_confirmation_can_name_a_single_tool(tmp_path: Path) -> None:
    built = toolset(tmp_path, confirm_names=["ShowTicket"])
    search, show = await built.get_tools(context())
    assert await search.check_require_confirmation({}, context()) is False
    assert await show.check_require_confirmation({}, context()) is True


def nested_comment_spec() -> dict[str, Any]:
    """UpdateTicket's body nested two levels deep: ticket.comment.public
    decides whether the requester is mailed."""
    return update_body(
        {
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "comment": {
                            "type": "object",
                            "properties": {
                                "body": {"type": "string"},
                                "public": {"type": "boolean"},
                            },
                        },
                    },
                }
            },
        }
    )


async def test_a_nested_fixed_value_is_not_in_the_declaration(
    tmp_path: Path,
) -> None:
    """Its value is declared, so there is nothing for the model to say,
    however deep it sits."""
    built = toolset(
        tmp_path,
        document=nested_comment_spec(),
        operations=["UpdateTicket"],
        effect="write",
        params={"UpdateTicket": {"ticket.comment.public": {"value": False}}},
    )
    update = (await built.get_tools(context()))[0]
    declared = update._get_declaration().model_dump_json(exclude_none=True)
    assert "comment" in declared and "body" in declared
    assert "public" not in declared


def test_build_refuses_a_declaration_the_description_cannot_carry(
    tmp_path: Path,
) -> None:
    with pytest.raises(DeclarationError, match="Nope"):
        toolset(tmp_path, operations=["Nope"])


def test_a_fix_the_description_cannot_place_fails_at_build(tmp_path: Path) -> None:
    """A constraint that silently never held would be worse than a failure."""
    with pytest.raises(DeclarationError, match="nested"):
        toolset(
            tmp_path,
            operations=["UpdateTicket"],
            effect="write",
            params={"UpdateTicket": {"nested": {"value": 1}}},
        )


class RecordingClient:
    """Stands in for the shared ConnectionClient and records every request."""

    def __init__(self, answer: Any = None) -> None:
        self.answer = answer if answer is not None else {"ok": True}
        self.calls: list[dict[str, Any]] = []

    async def get_json(self, url: str, params: Any = None, **kwargs: Any) -> Any:
        return self._record("GET", url, params, None, kwargs)

    async def post_json(
        self, url: str, body: Any = None, params: Any = None, **kwargs: Any
    ) -> Any:
        return self._record("POST", url, params, body, kwargs)

    async def put_json(
        self, url: str, body: Any = None, params: Any = None, **kwargs: Any
    ) -> Any:
        return self._record("PUT", url, params, body, kwargs)

    async def patch_json(
        self, url: str, body: Any = None, params: Any = None, **kwargs: Any
    ) -> Any:
        return self._record("PATCH", url, params, body, kwargs)

    async def delete_json(self, url: str, params: Any = None, **kwargs: Any) -> Any:
        return self._record("DELETE", url, params, None, kwargs)

    def _record(
        self, method: str, url: str, params: Any, body: Any, kwargs: Any
    ) -> Any:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "body": body,
                "state": kwargs.get("state"),
            }
        )
        return self.answer


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> RecordingClient:
    recorder = RecordingClient()
    monkeypatch.setattr(
        "gete.runtime.openapi.shared_client", lambda connection_id: recorder
    )
    return recorder


async def run(built: OpenApiToolset, name: str, args: dict[str, Any]) -> Any:
    tool = next(t for t in await built.get_tools(context()) if t.name == name)
    return await tool.run_async(args=args, tool_context=context())


async def test_requests_go_under_the_connections_base_url_never_the_specs(
    tmp_path: Path, client: RecordingClient
) -> None:
    built = toolset(tmp_path)
    result = await run(built, "ListSearchResults", {"query": "invoice"})
    assert result == {"ok": True}
    call = client.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://acme.example.com/search"
    assert call["params"] == {"query": "invoice"}


async def test_a_prefix_is_put_in_front_of_what_the_model_wrote(
    tmp_path: Path, client: RecordingClient
) -> None:
    built = toolset(
        tmp_path,
        params={"ListSearchResults": {"query": {"prefix": "type:ticket "}}},
    )
    await run(built, "ListSearchResults", {"query": "type:user urgent"})
    # The declared kind comes first, so the model's own type: does not win.
    assert client.calls[0]["params"] == {"query": "type:ticket type:user urgent"}


async def test_a_fixed_value_rides_on_every_request(
    tmp_path: Path, client: RecordingClient
) -> None:
    built = toolset(
        tmp_path,
        params={"ListSearchResults": {"per_page": {"value": 25}}},
    )
    # The model cannot see per_page; even a value smuggled into the
    # arguments is overridden by the declared one.
    await run(built, "ListSearchResults", {"query": "x", "per_page": 100})
    assert client.calls[0]["params"] == {"query": "x", "per_page": 25}


async def test_a_path_parameter_cannot_climb_out_of_its_segment(
    tmp_path: Path, client: RecordingClient
) -> None:
    built = toolset(tmp_path)
    await run(built, "ShowTicket", {"ticket_id": "1/../../admin"})
    assert client.calls[0]["url"] == (
        "https://acme.example.com/tickets/1%2F..%2F..%2Fadmin"
    )


async def test_a_put_operation_sends_the_json_body_with_the_put_verb(
    tmp_path: Path, client: RecordingClient
) -> None:
    built = toolset(tmp_path, operations=["UpdateTicket"], effect="write")
    await run(built, "UpdateTicket", {"ticket_id": "7", "ticket": {"status": "solved"}})
    call = client.calls[0]
    assert call["method"] == "PUT"
    assert call["url"] == "https://acme.example.com/tickets/7"
    assert call["body"] == {"ticket": {"status": "solved"}}


def update_body(schema: dict[str, Any], **components: Any) -> dict[str, Any]:
    """SPEC with UpdateTicket's body schema replaced, plus extra components."""
    spec = copy.deepcopy(SPEC)
    spec["paths"]["/tickets/{ticket_id}"]["put"]["requestBody"]["content"][
        "application/json"
    ]["schema"] = schema
    if components:
        spec["components"] = {"schemas": components}
    return spec


async def test_a_body_whose_type_is_left_implicit_sends_its_properties(
    tmp_path: Path, client: RecordingClient
) -> None:
    """Published schemas often leave type: object implicit; the payload must
    not end up wrapped under a body argument the service never declared."""
    spec = update_body({"properties": {"ticket": {"type": "object"}}})
    built = toolset(
        tmp_path, document=spec, operations=["UpdateTicket"], effect="write"
    )
    update = (await built.get_tools(context()))[0]
    declared = update._get_declaration().model_dump_json(exclude_none=True)
    assert '"ticket"' in declared
    assert '"body"' not in declared
    await run(built, "UpdateTicket", {"ticket_id": "7", "ticket": {"status": "solved"}})
    assert client.calls[0]["body"] == {"ticket": {"status": "solved"}}


async def test_an_allof_body_sends_its_properties_at_the_top_level(
    tmp_path: Path, client: RecordingClient
) -> None:
    spec = update_body(
        {
            "allOf": [
                {"$ref": "#/components/schemas/TicketCore"},
                {"properties": {"comment": {"type": "string"}}},
            ]
        },
        TicketCore={"type": "object", "properties": {"status": {"type": "string"}}},
    )
    built = toolset(
        tmp_path, document=spec, operations=["UpdateTicket"], effect="write"
    )
    await run(
        built, "UpdateTicket", {"ticket_id": "7", "status": "open", "comment": "hi"}
    )
    assert client.calls[0]["body"] == {"status": "open", "comment": "hi"}


async def test_a_nested_fixed_value_rides_wherever_its_parent_is_sent(
    tmp_path: Path, client: RecordingClient
) -> None:
    """Even a value smuggled into the nested object is overridden by the
    declared one."""
    built = toolset(
        tmp_path,
        document=nested_comment_spec(),
        operations=["UpdateTicket"],
        effect="write",
        params={"UpdateTicket": {"ticket.comment.public": {"value": False}}},
    )
    await run(
        built,
        "UpdateTicket",
        {
            "ticket_id": "7",
            "ticket": {"comment": {"body": "done", "public": True}},
        },
    )
    assert client.calls[0]["body"] == {
        "ticket": {"comment": {"body": "done", "public": False}}
    }


async def test_a_nested_fixed_value_never_conjures_its_parent_up(
    tmp_path: Path, client: RecordingClient
) -> None:
    """No comment written means no comment sent; an empty comment carrying
    only the fix would be a write the model never made."""
    built = toolset(
        tmp_path,
        document=nested_comment_spec(),
        operations=["UpdateTicket"],
        effect="write",
        params={"UpdateTicket": {"ticket.comment.public": {"value": False}}},
    )
    await run(built, "UpdateTicket", {"ticket_id": "7", "ticket": {"status": "solved"}})
    assert client.calls[0]["body"] == {"ticket": {"status": "solved"}}


async def test_a_nested_prefix_wraps_what_the_model_wrote(
    tmp_path: Path, client: RecordingClient
) -> None:
    built = toolset(
        tmp_path,
        document=nested_comment_spec(),
        operations=["UpdateTicket"],
        effect="write",
        params={"UpdateTicket": {"ticket.comment.body": {"prefix": "[agent] "}}},
    )
    await run(
        built,
        "UpdateTicket",
        {"ticket_id": "7", "ticket": {"comment": {"body": "done"}}},
    )
    assert client.calls[0]["body"] == {"ticket": {"comment": {"body": "[agent] done"}}}


async def test_a_nested_fix_on_a_parent_that_is_no_object_refuses_the_request(
    tmp_path: Path, client: RecordingClient
) -> None:
    """The declaration says an object sits there; sending the request around
    the fix would drop the constraint on the floor."""
    built = toolset(
        tmp_path,
        document=nested_comment_spec(),
        operations=["UpdateTicket"],
        effect="write",
        params={"UpdateTicket": {"ticket.comment.public": {"value": False}}},
    )
    with pytest.raises(GeteError, match="ticket.comment"):
        await run(
            built, "UpdateTicket", {"ticket_id": "7", "ticket": {"comment": "done"}}
        )
    assert client.calls == []


async def test_only_takes_the_unlisted_out_of_declaration_and_request(
    tmp_path: Path, client: RecordingClient
) -> None:
    """What only leaves out is not narrowed but gone: not shown, and not
    sent even when smuggled into the arguments."""
    built = toolset(
        tmp_path,
        only={"ListSearchResults": ["query"]},
    )
    search = next(
        t for t in await built.get_tools(context()) if t.name == "ListSearchResults"
    )
    declared = search._get_declaration().model_dump_json(exclude_none=True)
    assert "query" in declared
    assert "per_page" not in declared
    await run(built, "ListSearchResults", {"query": "x", "per_page": 100})
    assert client.calls[0]["params"] == {"query": "x"}


async def test_a_dotted_only_entry_narrows_the_declared_body(
    tmp_path: Path, client: RecordingClient
) -> None:
    built = toolset(
        tmp_path,
        document=nested_comment_spec(),
        operations=["UpdateTicket"],
        effect="write",
        only={"UpdateTicket": ["ticket_id", "ticket.comment.body"]},
    )
    update = (await built.get_tools(context()))[0]
    declared = update._get_declaration().model_dump_json(exclude_none=True)
    assert "comment" in declared and "body" in declared
    assert "status" not in declared and "public" not in declared


async def test_a_dotted_only_entry_filters_what_the_model_smuggles(
    tmp_path: Path, client: RecordingClient
) -> None:
    """The declaration wins over what the model wrote, as it does for a
    smuggled fixed parameter."""
    built = toolset(
        tmp_path,
        document=nested_comment_spec(),
        operations=["UpdateTicket"],
        effect="write",
        only={"UpdateTicket": ["ticket_id", "ticket.comment.body"]},
    )
    await run(
        built,
        "UpdateTicket",
        {
            "ticket_id": "7",
            "ticket": {
                "status": "closed",
                "comment": {"body": "done", "public": True},
            },
        },
    )
    assert client.calls[0]["body"] == {"ticket": {"comment": {"body": "done"}}}


async def test_a_fixed_value_still_rides_when_only_leaves_it_out(
    tmp_path: Path, client: RecordingClient
) -> None:
    """only says what the model may write; what the declaration fixed is the
    declaration's, and rides regardless."""
    built = toolset(
        tmp_path,
        only={"ListSearchResults": ["query"]},
        params={"ListSearchResults": {"per_page": {"value": 25}}},
    )
    await run(built, "ListSearchResults", {"query": "x"})
    assert client.calls[0]["params"] == {"query": "x", "per_page": 25}


async def test_only_and_a_nested_fix_compose(
    tmp_path: Path, client: RecordingClient
) -> None:
    """The helpdesk case in full: the model writes nothing but the comment
    text, and the declaration keeps the comment internal."""
    built = toolset(
        tmp_path,
        document=nested_comment_spec(),
        operations=["UpdateTicket"],
        effect="write",
        only={"UpdateTicket": ["ticket_id", "ticket.comment.body"]},
        params={"UpdateTicket": {"ticket.comment.public": {"value": False}}},
    )
    await run(
        built,
        "UpdateTicket",
        {"ticket_id": "7", "ticket": {"comment": {"body": "done", "public": True}}},
    )
    assert client.calls[0]["body"] == {
        "ticket": {"comment": {"body": "done", "public": False}}
    }


async def test_a_dotted_parameter_listed_in_only_stays_offered(
    tmp_path: Path, client: RecordingClient
) -> None:
    """A dot in a listed name is only a path when nothing carries the name
    literally, exactly as params reads it."""
    document = copy.deepcopy(SPEC)
    document["paths"]["/search"]["get"]["parameters"].append(
        {"name": "page.size", "in": "query", "schema": {"type": "integer"}}
    )
    built = toolset(
        tmp_path,
        document=document,
        operations=["ListSearchResults"],
        only={"ListSearchResults": ["query", "page.size"]},
    )
    search = (await built.get_tools(context()))[0]
    offered = {a.name: a.py_name for a in search._arguments if not a.fixed}
    assert "page.size" in offered
    await run(built, "ListSearchResults", {"query": "x", offered["page.size"]: 5})
    assert client.calls[0]["params"] == {"query": "x", "page.size": 5}


async def test_a_parent_the_filter_empties_is_not_sent(
    tmp_path: Path, client: RecordingClient
) -> None:
    """Nothing of the write survived the filter; an emptied ticket riding
    along would be a write the model never made."""
    built = toolset(
        tmp_path,
        document=nested_comment_spec(),
        operations=["UpdateTicket"],
        effect="write",
        only={"UpdateTicket": ["ticket_id", "ticket.comment.body"]},
    )
    await run(built, "UpdateTicket", {"ticket_id": "7", "ticket": {"status": "x"}})
    assert client.calls[0]["body"] is None


async def test_a_delete_operation_uses_the_delete_verb(
    tmp_path: Path, client: RecordingClient
) -> None:
    built = toolset(tmp_path, operations=["DeleteTicket"], effect="write")
    await run(built, "DeleteTicket", {"ticket_id": "7"})
    call = client.calls[0]
    assert call["method"] == "DELETE"
    assert call["url"] == "https://acme.example.com/tickets/7"
    assert call["body"] is None


async def test_the_callers_state_reaches_the_client(
    tmp_path: Path, client: RecordingClient
) -> None:
    """The client takes the token from the state; without it every user of
    the instance would share whatever call came first."""
    built = toolset(tmp_path)
    await run(built, "ListSearchResults", {"query": "x"})
    assert client.calls[0]["state"] == {"mail-triage-rooted-api": TOKEN}


def test_build_turns_the_declaration_into_a_toolset(project: ProjectBuilder) -> None:
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "connections": {
                "rooted-api": {k: v for k, v in ROOTED_API.items() if k != "id"}
            },
        }
    )
    directory = project.write_agent(
        "mail-triage",
        {
            "connections": ["rooted-api"],
            "tools": [
                {
                    "openapi": {
                        "spec": "./spec.yaml",
                        "connection": "rooted-api",
                        "operations": ["ListSearchResults"],
                        "effect": "read",
                    }
                }
            ],
        },
    )
    (directory / "spec.yaml").write_text(yaml.safe_dump(SPEC, sort_keys=False))
    loaded = load_project(project.root / "gete.yaml")
    resolved = resolve(loaded, loaded.agents[0])
    path = directory / RESOLVED_FILE
    path.write_text(yaml.safe_dump(resolved, sort_keys=False))
    built, asking = build(path).tools
    assert isinstance(built, OpenApiToolset)
    assert built.connection_id == "rooted-api"
    # The connection joins the reauthorization tool like an MCP one would.
    assert isinstance(asking, ReauthorizationToolset)
    assert asking.connection_ids == ("rooted-api",)
