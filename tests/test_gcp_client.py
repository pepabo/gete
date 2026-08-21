"""The REST client every GCP call goes through."""

import json
from collections.abc import Callable

import httpx
import pytest

from gete.gcp import GcpClient, GcpError


def client(handler: Callable[[httpx.Request], httpx.Response]) -> GcpClient:
    return GcpClient(
        quota_project="example-project",
        token_provider=lambda: "tok-123",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda seconds: None,
    )


def test_requests_carry_the_token_and_the_quota_project() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    assert client(handler).get("https://example.googleapis.com/v1/x") == {"ok": True}
    assert seen[0].headers["Authorization"] == "Bearer tok-123"
    assert seen[0].headers["X-Goog-User-Project"] == "example-project"


def test_bodies_are_json_and_params_become_the_query() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client(handler).patch(
        "https://example.googleapis.com/v1/x", {"a": 1}, params={"updateMask": "a"}
    )
    assert seen[0].method == "PATCH"
    assert seen[0].headers["Content-Type"] == "application/json"
    assert json.loads(seen[0].content) == {"a": 1}
    assert seen[0].url.params["updateMask"] == "a"


def test_errors_carry_status_and_the_start_of_the_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"message": "license is not available"}}
        )

    with pytest.raises(GcpError) as caught:
        client(handler).post("https://example.googleapis.com/v1/x", {})
    assert caught.value.status == 403
    assert "license is not available" in str(caught.value)


def test_empty_responses_read_as_empty_dicts() -> None:
    assert (
        client(lambda request: httpx.Response(200, content=b"")).delete("https://e.g/x")
        == {}
    )


def test_list_all_follows_page_tokens() -> None:
    pages = {
        None: {"items": [{"n": 1}], "nextPageToken": "p2"},
        "p2": {"items": [{"n": 2}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("pageToken")])

    assert client(handler).list_all("https://e.g/items", "items") == [
        {"n": 1},
        {"n": 2},
    ]


def test_wait_operation_polls_until_done_and_returns_the_response() -> None:
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        polls += 1
        done = polls >= 3
        body = {"name": "operations/1", "done": done}
        if done:
            body["response"] = {"name": "engines/9"}
        return httpx.Response(200, json=body)

    result = client(handler).wait_operation("https://e.g/v1", {"name": "operations/1"})
    assert result == {"name": "engines/9"}
    assert polls == 3


def test_wait_operation_raises_on_an_operation_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"name": "operations/1", "done": True, "error": {"message": "boom"}},
        )

    with pytest.raises(GcpError, match="boom"):
        client(handler).wait_operation("https://e.g/v1", {"name": "operations/1"})
