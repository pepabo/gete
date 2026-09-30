"""Installation tokens gete issues from a GitHub App's private key."""

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from gete.connection import Connection, Registry
from gete.connection.github_app import AppTokenUnavailable, InstallationTokens
from gete.errors import UserFacingError

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = PRIVATE_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()

NOW = 1_800_000_000.0
TOKEN = "ghs_16C7e42F292c6912E7710c838347Ae178B4a"
API = "https://api.github.com"


def app_connection(**override: Any) -> Connection:
    registry = Registry.from_catalog(
        {
            "github-app": {
                "app": {
                    "app_id": "123",
                    "private_key_secret": "ge-github-app-private-key",
                    "repositories": ["example-org/requests", "example-org/other"],
                    "permissions": {"issues": "read"},
                },
                **override,
            }
        }
    )
    return registry.get("github-app")


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class GitHub:
    """The two endpoints an installation token takes, answering as told."""

    def __init__(self, root: str = API) -> None:
        self.root = root
        self.requests: list[httpx.Request] = []
        self.installations: dict[str, httpx.Response] = {
            "example-org/requests": httpx.Response(200, json={"id": 42})
        }
        self.issued: list[httpx.Response] = []
        self.expires_at = iso(NOW + 3600)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix(httpx.URL(self.root).path.rstrip("/"))
        if path.startswith("/repos/") and path.endswith("/installation"):
            repository = path.removeprefix("/repos/").removesuffix("/installation")
            return self.installations.get(repository, httpx.Response(404))
        if path == "/app/installations/42/access_tokens":
            if self.issued:
                return self.issued.pop(0)
            return httpx.Response(
                201, json={"token": TOKEN, "expires_at": self.expires_at}
            )
        return httpx.Response(404)

    def token_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/access_tokens")]


def issuer(
    github: GitHub,
    *,
    connection: Connection | None = None,
    environ: dict[str, str] | None = None,
    clock: Callable[[], float] = lambda: NOW,
) -> InstallationTokens:
    return InstallationTokens(
        connection or app_connection(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(github)),
        environ={"GETE_APP_KEY_GITHUB_APP": PEM} if environ is None else environ,
        clock=clock,
    )


def claims_of(jwt: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Header and claims, after checking the signature against the App's key."""
    header, payload, signature = jwt.split(".")

    def decode(part: str) -> bytes:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    PRIVATE_KEY.public_key().verify(
        decode(signature),
        f"{header}.{payload}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return json.loads(decode(header)), json.loads(decode(payload))


async def test_the_app_signs_in_with_a_short_lived_rs256_jwt() -> None:
    github = GitHub()
    await issuer(github).token()
    authorization = github.requests[0].headers["Authorization"]
    assert authorization.startswith("Bearer ")
    header, claims = claims_of(authorization.removeprefix("Bearer "))
    assert header["alg"] == "RS256"
    assert claims["iss"] == "123"
    # Backdated for a clock that runs ahead of GitHub's, and short of the ten
    # minutes GitHub accepts at most.
    assert claims["iat"] < NOW
    assert claims["exp"] - claims["iat"] < 600


async def test_the_installation_is_found_from_a_permitted_repository() -> None:
    github = GitHub()
    await issuer(github).token()
    assert github.requests[0].method == "GET"
    assert str(github.requests[0].url) == (
        f"{API}/repos/example-org/requests/installation"
    )


async def test_a_repository_the_app_is_not_installed_on_is_passed_over() -> None:
    github = GitHub()
    github.installations = {
        "example-org/other": httpx.Response(200, json={"id": 42}),
    }
    assert await issuer(github).token() == TOKEN


async def test_the_token_is_narrowed_to_the_declared_ceiling() -> None:
    github = GitHub()
    assert await issuer(github).token() == TOKEN
    [request] = github.token_requests()
    assert request.method == "POST"
    assert json.loads(request.content) == {
        "repositories": ["requests", "other"],
        "permissions": {"issues": "read"},
    }
    # Signed in as the App, never with a token it issued.
    assert request.headers["Authorization"] != f"Bearer {TOKEN}"


async def test_the_token_is_reused_until_shortly_before_it_expires() -> None:
    github = GitHub()
    now = [NOW]
    tokens = issuer(github, clock=lambda: now[0])
    await tokens.token()
    now[0] = NOW + 3600 - 600
    await tokens.token()
    assert len(github.token_requests()) == 1
    now[0] = NOW + 3600 - 60
    await tokens.token()
    assert len(github.token_requests()) == 2
    # The installation does not move; it is looked up once.
    assert sum(r.url.path.endswith("/installation") for r in github.requests) == 1


async def test_a_forgotten_token_is_issued_again() -> None:
    github = GitHub()
    tokens = issuer(github)
    await tokens.token()
    tokens.forget()
    await tokens.token()
    assert len(github.token_requests()) == 2


async def test_github_enterprise_is_reached_below_its_own_root() -> None:
    root = "https://ghe.example.com/api/v3"
    github = GitHub(root)
    connection = app_connection(base_url=root)
    await issuer(github, connection=connection).token()
    assert str(github.requests[0].url).startswith(f"{root}/repos/")
    assert str(github.token_requests()[0].url) == (
        f"{root}/app/installations/42/access_tokens"
    )


async def test_without_the_key_nothing_is_sent_and_the_reason_is_text() -> None:
    github = GitHub()
    with pytest.raises(AppTokenUnavailable) as raised:
        await issuer(github, environ={}).token()
    assert isinstance(raised.value, UserFacingError)
    assert "GETE_APP_KEY_GITHUB_APP" in str(raised.value)
    assert github.requests == []


async def test_a_key_that_is_not_a_private_key_is_reported_without_its_content() -> (
    None
):
    github = GitHub()
    with pytest.raises(AppTokenUnavailable) as raised:
        await issuer(github, environ={"GETE_APP_KEY_GITHUB_APP": "not-a-key"}).token()
    assert "not-a-key" not in str(raised.value)
    assert github.requests == []


async def test_an_app_installed_on_none_of_the_repositories_is_reported() -> None:
    github = GitHub()
    github.installations = {}
    with pytest.raises(AppTokenUnavailable, match="example-org/requests"):
        await issuer(github).token()


@pytest.mark.parametrize("status", [401, 403, 422])
async def test_a_refused_issue_is_reported_with_the_status_only(status: int) -> None:
    """The body may name what the App was denied; the status is diagnosis enough."""
    github = GitHub()
    github.issued = [httpx.Response(status, json={"message": "secret detail"})]
    with pytest.raises(AppTokenUnavailable) as raised:
        await issuer(github).token()
    assert str(status) in str(raised.value)
    assert "secret detail" not in str(raised.value)


async def test_a_token_of_another_shape_is_refused() -> None:
    """Whatever came back is sent on as this connection's token; it has to
    look like one."""
    github = GitHub()
    github.issued = [
        httpx.Response(201, json={"token": "gho_x", "expires_at": iso(NOW + 3600)})
    ]
    with pytest.raises(AppTokenUnavailable):
        await issuer(github).token()


async def test_an_unreachable_github_is_reported_as_text() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    tokens = InstallationTokens(
        app_connection(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
        environ={"GETE_APP_KEY_GITHUB_APP": PEM},
        clock=lambda: NOW,
    )
    with pytest.raises(AppTokenUnavailable):
        await tokens.token()


async def test_an_answer_that_is_not_json_is_reported_as_text() -> None:
    github = GitHub()
    github.issued = [httpx.Response(201, content=b"<html>")]
    with pytest.raises(AppTokenUnavailable):
        await issuer(github).token()
    github = GitHub()
    github.installations = {"example-org/requests": httpx.Response(200, content=b"{")}
    with pytest.raises(AppTokenUnavailable):
        await issuer(github).token()
