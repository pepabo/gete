"""Reading an external service with the user's token.

The client only reads. Writes belong to the agent, split into a tool that
shows what will happen and one that does it.

Retries, re-authorization, and the destination check live here once. Written
per connection, the one that gets it wrong leaves the user with a failure
and no reason. Everything is asynchronous: ADK calls synchronous tools inline,
so a blocking wait would stall every request on the instance.
"""

import asyncio
import datetime
import email.utils
import logging
import math
import types
from functools import cache
from typing import Any, Self

import httpx

from gete.connection.registry import Connection
from gete.connection.runtime import caller_token, resolve_connection
from gete.errors import GeteError
from gete.redact import RedactRules, redact
from gete.request_context import current_tool_call

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
# Transient failures usually clear within a few attempts; waiting longer
# keeps the caller hanging.
MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 1.0
# A server may ask for a long pause, but a tool call cannot wait that long.
MAX_RETRY_AFTER_SECONDS = 60.0
# Attachments above this are not read; a person looks at them instead.
MAX_FILE_BYTES = 20 * 1024 * 1024


class ExternalServiceError(GeteError):
    """The external service could not be read."""


class ReauthorizationRequired(ExternalServiceError):
    """No usable token; the user has to authorize again in Gemini Enterprise."""


def parse_retry_after(header: str | None, default: float) -> float:
    """Retry-After is either seconds or an HTTP-date (RFC 9110)."""
    if not header:
        return default
    try:
        seconds = float(header)
    except ValueError:
        pass
    else:
        # "nan" parses as a float and asyncio.sleep(nan) never wakes up, so a
        # header from the service could hold the tool call forever.
        if math.isnan(seconds):
            logger.warning("Retry-After %r is not a delay; using the default", header)
            return default
        return _within_bounds(seconds)
    try:
        parsed = email.utils.parsedate_to_datetime(header)
    except (TypeError, ValueError):
        logger.warning(
            "could not parse Retry-After %r; using the default delay", header
        )
        return default
    return _within_bounds(
        (parsed - datetime.datetime.now(parsed.tzinfo)).total_seconds()
    )


def _within_bounds(seconds: float) -> float:
    """A delay that can be waited: never negative, never longer than the cap."""
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


@cache
def shared_client(connection_id: str) -> "ConnectionClient":
    """One client per connection, reusing its connections.

    The token is taken per request from the running tool call, not stored on
    the client, so users never share one.
    """
    return ConnectionClient(connection_id)


class ConnectionClient:
    """Reads a declared connection with the calling user's token."""

    def __init__(
        self,
        target: Connection | str,
        client: httpx.AsyncClient | None = None,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        # An id is resolved at request time against the running call's
        # registry; shared_client() is created before any call exists.
        self._target = target
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
        self._backoff = backoff_seconds

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_json(
        self, url: str, params: dict[str, Any] | None = None, state: Any = None
    ) -> Any:
        """GET and return the JSON body after the policies' redaction."""
        response = await self._request(url, params, state=state)
        call = current_tool_call()
        rules = (
            call.redact_rules
            if call is not None and call.redact_rules
            else RedactRules()
        )
        return redact(response.json(), rules)

    async def get_bytes(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        max_bytes: int = MAX_FILE_BYTES,
        state: Any = None,
    ) -> bytes:
        """GET a file, following redirects; httpx drops Authorization across hosts."""
        response = await self._request(url, params, follow_redirects=True, state=state)
        if len(response.content) > max_bytes:
            raise ExternalServiceError(
                f"the response is too large ({len(response.content)} bytes)"
            )
        return response.content

    def _connection(self) -> Connection:
        return resolve_connection(self._target)

    def _authorization(
        self, connection: Connection, url: str, state: Any
    ) -> dict[str, str]:
        token = caller_token(connection, state)
        if token is None:
            # The user sees a re-authorization prompt; operators would not.
            logger.warning(
                "not reading %s without the caller's token url=%s", connection.id, url
            )
            raise ReauthorizationRequired(connection.reauthorization_message())
        return {"Authorization": f"Bearer {token}"}

    async def _request(
        self,
        url: str,
        params: dict[str, Any] | None,
        *,
        follow_redirects: bool = False,
        state: Any = None,
    ) -> httpx.Response:
        connection = self._connection()
        if not connection.allows(url):
            raise ExternalServiceError(
                f"{url} is not a {connection.display_name} host; the token stays here"
            )
        headers = self._authorization(connection, url, state)
        # Who read what is the service's audit log's business. The token is
        # the user's credential and the query is the user's work; neither is
        # logged.
        logger.info("reading %s url=%s", connection.id, url)

        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self._client.get(
                    url,
                    params=params,
                    headers=headers,
                    follow_redirects=follow_redirects,
                )
            except httpx.HTTPError as error:
                if attempt == MAX_ATTEMPTS - 1:
                    raise ExternalServiceError(
                        f"could not connect to {connection.display_name}: {error}"
                    ) from error
                await asyncio.sleep(self._backoff)
                continue

            if response.status_code == 401:
                # There is no way to refresh; authorization is Gemini Enterprise's job.
                logger.warning("token for %s was rejected url=%s", connection.id, url)
                # The distinction between "never authorized" and "expired"
                # lives in the logs; users get one prompt either way, in the
                # language the connection declares.
                raise ReauthorizationRequired(connection.reauthorization_message())
            if response.status_code == 429:
                if attempt == MAX_ATTEMPTS - 1:
                    raise ExternalServiceError(
                        f"{connection.display_name} kept rate limiting the request"
                    )
                await asyncio.sleep(
                    parse_retry_after(
                        response.headers.get("Retry-After"), self._backoff
                    )
                )
                continue
            if response.status_code >= 500:
                if attempt == MAX_ATTEMPTS - 1:
                    raise ExternalServiceError(
                        f"{connection.display_name} answered {response.status_code}"
                    )
                await asyncio.sleep(self._backoff)
                continue
            if response.status_code >= 400:
                raise ExternalServiceError(
                    f"{connection.display_name} answered {response.status_code}: "
                    f"{response.text[:200]}"
                )
            return response
        raise ExternalServiceError(f"{connection.display_name}: too many attempts")
