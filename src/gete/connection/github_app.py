"""Installation tokens gete issues itself, from a GitHub App's private key.

A connection usually reads with the token Gemini Enterprise forwards for the
calling user. An app connection has no such token: the deployment holds the
App's key, signs in as the App, and asks GitHub for an installation token
narrowed to the repositories and permissions gete.yaml declares. Whoever can
call the agent acts as the App within that ceiling, so the ceiling is sent
with every issue rather than left to the installation's grant.

The key and the App's JWT only travel to the connection's own hosts, and
nothing about either is logged. A token is reused until shortly before it
expires; GitHub issues them for an hour.
"""

import asyncio
import base64
import json
import logging
import os
import time
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from gete.connection.registry import Connection
from gete.errors import UserFacingError

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30.0
# GitHub refuses an App JWT valid for more than ten minutes, and one issued
# in the future by its own clock; backdating iat covers a deployment's clock
# running ahead, and the lifetime is counted from the backdated iat.
JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 9 * 60
# A token handed out this close to its expiry could lapse in the middle of
# the request it was issued for.
REISSUE_BEFORE_SECONDS = 5 * 60
ACCEPT = "application/vnd.github+json"


class AppTokenUnavailable(UserFacingError):
    """No installation token could be issued.

    UserFacingError because every message is written here from the
    connection's declaration and a status code: never a response body, never
    anything read from the key.
    """


class InstallationTokens:
    """Issues and reuses the installation tokens of one app connection."""

    def __init__(
        self,
        connection: Connection,
        *,
        client: httpx.AsyncClient | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if connection.app is None:
            raise ValueError(f"connection {connection.id} is not an app connection")
        self._connection = connection
        self._app = connection.app
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        self._environ = os.environ if environ is None else environ
        self._clock = clock
        self._lock = asyncio.Lock()
        self._installation: int | None = None
        self._token: str | None = None
        self._expires_at = 0.0

    async def token(self) -> str:
        """A token the connection accepts, issued when the last one is near expiry."""
        async with self._lock:
            if self._token is None or self._clock() >= (
                self._expires_at - REISSUE_BEFORE_SECONDS
            ):
                self._token, self._expires_at = await self._issue()
            return self._token

    def forget(self) -> None:
        """Drop the token held, so the next request is issued a new one.

        For a token the service refused before its time: holding on to it
        would refuse every request until it expired.
        """
        self._token = None

    async def _issue(self) -> tuple[str, float]:
        headers = {"Authorization": f"Bearer {self._jwt()}", "Accept": ACCEPT}
        if self._installation is None:
            self._installation = await self._find_installation(headers)
        response = await self._send(
            "POST",
            f"/app/installations/{self._installation}/access_tokens",
            headers,
            {
                # The installation's own grant is the most a token could
                # carry; asking for the declared ceiling every time is what
                # keeps a token from carrying it.
                "repositories": [
                    repository.partition("/")[2]
                    for repository in self._app.repositories
                ],
                "permissions": dict(self._app.permissions),
            },
        )
        if response.status_code != 201:
            self._installation = None
            raise self._unavailable(
                f"GitHub refused to issue an installation token "
                f"({response.status_code})"
            )
        payload = _json_object(response)
        token = payload.get("token")
        if not isinstance(token, str) or not self._connection.accepts_token(token):
            raise self._unavailable(
                "GitHub answered with something that is not an installation token"
            )
        try:
            expires_at = datetime.fromisoformat(str(payload["expires_at"]))
        except (KeyError, ValueError):
            raise self._unavailable(
                "GitHub issued a token without saying when it expires"
            ) from None
        logger.info("issued an installation token for %s", self._connection.id)
        return token, expires_at.timestamp()

    async def _find_installation(self, headers: Mapping[str, str]) -> int:
        """The installation on the first declared repository the App is on.

        validate holds the repositories to one owner, so whichever answers
        is the one installation every one of them is reached through.
        """
        for repository in self._app.repositories:
            owner, _, name = repository.partition("/")
            response = await self._send(
                "GET",
                f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}"
                "/installation",
                headers,
            )
            if response.status_code == 404:
                continue
            if response.status_code != 200:
                raise self._unavailable(
                    f"GitHub refused to name the App's installation "
                    f"({response.status_code})"
                )
            installation = _json_object(response).get("id")
            if isinstance(installation, int):
                return installation
        raise self._unavailable(
            f"the App is installed on none of {', '.join(self._app.repositories)}"
        )

    async def _send(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: Any = None,
    ) -> httpx.Response:
        root = self._connection.base_url
        url = f"{(root or '').rstrip('/')}{path}"
        if root is None or not self._connection.allows(url):
            # The JWT signs in as the App; it goes where the tokens go or
            # nowhere.
            raise self._unavailable("the connection names no root on its own hosts")
        try:
            return await self._client.request(method, url, headers=headers, json=body)
        except httpx.HTTPError as error:
            raise self._unavailable(
                f"could not reach GitHub ({type(error).__name__})"
            ) from None

    def _jwt(self) -> str:
        """The App's own credential, signed with the key the deployment holds."""
        if not self._app.app_id:
            raise self._unavailable("no App is declared for it")
        pem = self._environ.get(self._connection.app_key_env)
        if not pem:
            raise self._unavailable(
                f"the App's private key is not in {self._connection.app_key_env}"
            )
        try:
            key = serialization.load_pem_private_key(pem.encode(), password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            # Whatever the parser says about the key, the key is the secret;
            # the message stays ours.
            key = None
        if not isinstance(key, rsa.RSAPrivateKey):
            raise self._unavailable(
                f"{self._connection.app_key_env} does not hold an RSA private key"
            )
        now = int(self._clock())
        issued_at = now - JWT_BACKDATE_SECONDS
        signing_input = ".".join(
            _base64url(json.dumps(part, separators=(",", ":")).encode())
            for part in (
                {"alg": "RS256", "typ": "JWT"},
                {
                    "iat": issued_at,
                    "exp": issued_at + JWT_LIFETIME_SECONDS,
                    "iss": self._app.app_id,
                },
            )
        )
        signature = key.sign(
            signing_input.encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        return f"{signing_input}.{_base64url(signature)}"

    def _unavailable(self, reason: str) -> AppTokenUnavailable:
        logger.warning("no installation token for %s: %s", self._connection.id, reason)
        return AppTokenUnavailable(
            f"{self._connection.display_name} is unavailable: {reason}. "
            "Ask the operator to check the App."
        )


def _json_object(response: httpx.Response) -> Mapping[str, Any]:
    """The answer's JSON object, or an empty one when it is anything else;
    the callers then find none of the fields they need and say so."""
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, Mapping) else {}


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


_issuers: dict[tuple[Any, ...], InstallationTokens] = {}


def installation_tokens(connection: Connection) -> InstallationTokens:
    """The process's issuer for this connection, shared by every tool call.

    Keyed by everything a token depends on, so a registry that declares the
    connection differently never reuses a token issued for another ceiling.
    """
    app = connection.app
    if app is None:
        raise ValueError(f"connection {connection.id} is not an app connection")
    key = (
        connection.id,
        connection.base_url,
        app.app_id,
        app.repositories,
        tuple(sorted(app.permissions.items())),
    )
    issuer = _issuers.get(key)
    if issuer is None:
        issuer = _issuers[key] = InstallationTokens(connection)
    return issuer
