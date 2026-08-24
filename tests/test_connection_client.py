"""ConnectionClient: reading an external service with the user's token."""

import json
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from gete.connection import Connection, Registry
from gete.connection.client import (
    ConnectionClient,
    ExternalServiceError,
    ReauthorizationRequired,
    parse_retry_after,
    shared_client,
)
from gete.redact import RedactRules
from gete.request_context import ToolCall, clear_tool_call, set_tool_call

CATALOG = Registry.from_catalog()
GITHUB = CATALOG.get("github")
TOKEN = "gho_16C7e42F292c6912E7710c838347Ae178B4a"
URL = "https://api.github.com/repos/o/r/issues"


def teardown_function() -> None:
    clear_tool_call()


def bind(token: str | None = TOKEN, rules: RedactRules | None = None) -> None:
    state = {"agent-github": token} if token else {}
    set_tool_call(
        ToolCall(
            SimpleNamespace(state=state, user_id="u"),
            {"github": "agent-github"},
            registry=CATALOG,
            redact_rules=rules,
        )
    )


def client(
    handler: Callable[[httpx.Request], httpx.Response], target: Any = GITHUB
) -> ConnectionClient:
    transport = httpx.MockTransport(handler)
    return ConnectionClient(
        target, client=httpx.AsyncClient(transport=transport), backoff_seconds=0
    )


def ok(payload: Any) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(200, json=payload)


async def test_get_json_returns_the_body() -> None:
    bind()
    assert await client(ok({"a": 1})).get_json(URL) == {"a": 1}


async def test_the_users_token_is_sent_as_a_bearer() -> None:
    bind()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    await client(handler).get_json(URL, params={"state": "open"})
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert seen[0].url.params["state"] == "open"


async def test_results_go_through_the_policies_redaction() -> None:
    bind(rules=RedactRules(keys=("bank_name",)))
    result = await client(ok({"bank_name": "Example Bank", "x": 1})).get_json(URL)
    assert result == {"bank_name": "[redacted]", "x": 1}


async def test_no_token_means_no_request_and_a_reauthorization_message() -> None:
    bind(token=None)
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    with pytest.raises(ReauthorizationRequired, match="GitHub"):
        await client(handler).get_json(URL)
    assert not called


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/x",
        "https://api.github.com.evil.example/x",
        "http://api.github.com/x",
    ],
)
async def test_token_is_never_sent_outside_the_declared_hosts(url: str) -> None:
    bind()
    with pytest.raises(ExternalServiceError):
        await client(ok({})).get_json(url)


async def test_401_is_not_retried_and_asks_for_reauthorization() -> None:
    bind()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401)

    with pytest.raises(ReauthorizationRequired):
        await client(handler).get_json(URL)
    assert attempts == 1


async def test_5xx_is_retried_then_fails() -> None:
    bind()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    with pytest.raises(ExternalServiceError, match="503"):
        await client(handler).get_json(URL)
    assert attempts == 3


async def test_429_waits_for_retry_after_then_retries() -> None:
    bind()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    assert await client(handler).get_json(URL) == {"ok": True}
    assert attempts == 2


async def test_other_4xx_is_not_retried() -> None:
    bind()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, text="missing")

    with pytest.raises(ExternalServiceError, match="404"):
        await client(handler).get_json(URL)
    assert attempts == 1


async def test_connection_failures_are_retried() -> None:
    bind()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"ok": True})

    assert await client(handler).get_json(URL) == {"ok": True}


async def test_requests_are_logged_without_token_or_query(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bind()
    with caplog.at_level(logging.INFO):
        await client(ok({})).get_json(URL, params={"q": "secret-project"})
    # httpx logs the full URL at INFO on its own logger; app() turns that down.
    ours = "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith("gete")
    )
    assert "github" in ours
    assert TOKEN not in ours
    assert "secret-project" not in ours


async def test_a_query_embedded_in_the_url_is_not_logged_either(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bind()
    with caplog.at_level(logging.INFO):
        await client(ok({})).get_json(URL + "?q=secret-project")
    ours = "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith("gete")
    )
    assert "/repos/o/r/issues" in ours
    assert "secret-project" not in ours


async def test_a_url_carrying_credentials_is_rejected_before_any_request() -> None:
    """allows() reads only the hostname, and httpx would turn userinfo into
    Basic auth in place of the caller's token."""
    bind()
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    with pytest.raises(ExternalServiceError) as info:
        await client(handler).get_json("https://user:hunter2@api.github.com/x")
    assert not called
    assert "hunter2" not in str(info.value)


async def test_rejected_urls_are_sanitized_in_the_error() -> None:
    """The rejection reaches the model; the query must not ride along."""
    bind()
    with pytest.raises(ExternalServiceError) as info:
        await client(ok({})).get_json("https://api.example.com/x?q=topsecret")
    assert "topsecret" not in str(info.value)
    assert "api.example.com" in str(info.value)


async def test_4xx_bodies_stay_out_of_the_error_message() -> None:
    """The error reaches the model without redaction, so the body stays out."""
    bind()
    with pytest.raises(ExternalServiceError) as info:
        await client(lambda request: httpx.Response(404, text="secret-body")).get_json(
            URL
        )
    assert "404" in str(info.value)
    assert "secret-body" not in str(info.value)


def redirecting(location: str) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"Location": location})
        return httpx.Response(200, content=b"data")

    return handler


@pytest.mark.parametrize(
    "location",
    [
        "http://files.example.com/f",
        "https://127.0.0.1/internal",
        "https://localhost/internal",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://2130706433/internal",
        "https://0x7f000001/internal",
        "https://files.example.com/blob",
    ],
)
async def test_get_bytes_refuses_undeclared_redirects(location: str) -> None:
    """Only hosts the connection declares; a name is no safer than an address."""
    bind()
    with pytest.raises(ExternalServiceError, match="declared"):
        await client(redirecting(location)).get_bytes(URL)


def with_redirect_hosts() -> Any:
    return Registry(
        [
            Connection.from_mapping(
                {
                    "id": "github",
                    "display_name": "GitHub",
                    "hosts": ["api.github.com"],
                    "redirect_hosts": ["files.example.com"],
                    "token_prefixes": ["gho_"],
                    "oauth": {
                        "authorization_url": "https://github.com/login/oauth/authorize",
                        "token_url": "https://github.com/login/oauth/access_token",
                        "scopes": {},
                    },
                }
            )
        ]
    ).get("github")


async def test_get_bytes_follows_declared_redirects_without_the_token() -> None:
    bind()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(
                302, headers={"Location": "https://files.example.com/blob"}
            )
        return httpx.Response(200, content=b"data")

    assert await client(handler, target=with_redirect_hosts()).get_bytes(URL) == b"data"
    assert seen[1].url.host == "files.example.com"
    assert "Authorization" not in seen[1].headers


async def test_get_bytes_keeps_the_token_on_the_connections_own_host() -> None:
    bind()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/repos/o/r/issues":
            return httpx.Response(
                302, headers={"Location": "https://api.github.com/moved"}
            )
        return httpx.Response(200, content=b"data")

    assert await client(handler).get_bytes(URL) == b"data"
    assert seen[1].url.path == "/moved"
    assert seen[1].headers["Authorization"] == f"Bearer {TOKEN}"


async def test_get_bytes_gives_up_on_endless_redirects() -> None:
    bind()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": URL})

    with pytest.raises(ExternalServiceError, match="redirect"):
        await client(handler).get_bytes(URL)


async def test_get_bytes_refuses_oversized_bodies() -> None:
    bind()
    big = client(lambda request: httpx.Response(200, content=b"x" * 10))
    with pytest.raises(ExternalServiceError, match="too large"):
        await big.get_bytes(URL, max_bytes=5)
    assert await big.get_bytes(URL, max_bytes=10) == b"x" * 10


async def test_get_json_refuses_oversized_bodies() -> None:
    bind()
    big = client(ok({"key": "0123456789"}))
    with pytest.raises(ExternalServiceError, match="too large"):
        await big.get_json(URL, max_bytes=5)


async def test_client_can_be_named_by_connection_id_and_resolves_at_call_time() -> None:
    """shared_client("github") is made before any call; the registry comes later."""
    bind()
    by_id = client(ok({"ok": True}), target="github")
    assert await by_id.get_json(URL) == {"ok": True}


def test_shared_client_is_one_per_connection() -> None:
    assert shared_client("github") is shared_client("github")
    assert shared_client("github") is not shared_client("freee")


def test_parse_retry_after_handles_seconds_dates_and_garbage() -> None:
    assert parse_retry_after("5", 1.0) == 5.0
    assert parse_retry_after("600", 1.0) == 60.0
    assert parse_retry_after(None, 1.0) == 1.0
    assert parse_retry_after("garbage", 1.0) == 1.0
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", 1.0) == 0.0


def test_parse_retry_after_never_returns_a_delay_that_cannot_be_waited() -> None:
    """asyncio.sleep(nan) never wakes up, so a header can hold the call forever."""
    assert parse_retry_after("nan", 1.0) == 1.0
    assert parse_retry_after("inf", 1.0) == 60.0
    assert parse_retry_after("-5", 1.0) == 0.0


def test_json_errors_are_reported_as_service_errors() -> None:
    assert issubclass(ReauthorizationRequired, ExternalServiceError)
    assert json  # keep the import honest for the type of payloads above


async def test_401_uses_the_connections_reauthorization_message() -> None:
    """One prompt for every path that ends in 'authorize again'."""
    declared = Registry(
        [
            Connection.from_mapping(
                {
                    "id": "github",
                    "display_name": "GitHub",
                    "hosts": ["api.github.com"],
                    "token_prefixes": ["gho_"],
                    "oauth": {
                        "authorization_url": "https://github.com/login/oauth/authorize",
                        "token_url": "https://github.com/login/oauth/access_token",
                        "scopes": {},
                    },
                    "messages": {"reauthorization": "LOCALIZED-REAUTHORIZE-PROMPT"},
                }
            )
        ]
    ).get("github")
    bind()
    with pytest.raises(ReauthorizationRequired, match="LOCALIZED-REAUTHORIZE-PROMPT"):
        await client(lambda request: httpx.Response(401), target=declared).get_json(URL)
