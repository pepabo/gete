"""Reaching an external service with the user's token.

The client carries requests, not judgement: a POST, PUT, or PATCH that
changes something is a write, governed by the declaration's effect and the
policies like any other, and a request that may already have been applied is
never sent twice. POST is also how many services read - search endpoints
usually take one - and updates are commonly PUT or PATCH.

DELETE is the sharpest of the verbs: what it removes, most services cannot
bring back, and afterwards its success and its failure read the same from
here - "not there". It is offered all the same, because a declared delete is
governed like every other write - the declaration's effect, the policies'
confirmation - where a tool denied the verb would reach for its own HTTP
client, outside every one of these guards.

Retries, re-authorization, and the destination check live here once. Written
per connection, the one that gets it wrong leaves the user with a failure
and no reason. Everything is asynchronous: ADK calls synchronous tools inline,
so a blocking wait would stall every request on the instance.
"""

import asyncio
import datetime
import email.utils
import http.cookiejar
import json
import logging
import math
import types
import urllib.parse
from collections.abc import Mapping
from functools import cache
from typing import Any, Self

import httpx

from gete.connection.registry import Connection
from gete.connection.runtime import caller_token, resolve_connection
from gete.errors import GeteError, UserFacingError
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
# A download needs a hop or two to a file host; a longer chain is a loop.
MAX_REDIRECTS = 5


class ExternalServiceError(GeteError):
    """The external service could not be read."""


class ReauthorizationRequired(ExternalServiceError, UserFacingError):
    """No token arrived; the user has to approve the connection in Gemini Enterprise.

    UserFacingError because its message is the connection's declared
    reauthorization prompt: configuration written to be shown, never data.
    """


class AuthorizationRefused(ExternalServiceError, UserFacingError):
    """A token arrived and the service refused it.

    Not a ReauthorizationRequired: Gemini Enterprise shows its consent screen
    only while it holds no credential for the user, and it never asks the
    provider whether the one it holds is still good. Told to approve again,
    the user finds nothing to approve; an operator has to reset the
    authorization. UserFacingError for the same reason as above - the message
    is the connection's declared text.
    """


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


def _loggable(url: str) -> str:
    """The URL reduced to scheme, host, port, and path.

    The path is routing; everything else the caller can write into a URL is
    theirs - the query is the user's work and userinfo is a credential - and
    both would land in the logs whenever the URL carries them.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        # urlsplit strips an IPv6 literal's brackets with the userinfo.
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    authority = host if port is None else f"{host}:{port}"
    return urllib.parse.urlunsplit((parts.scheme, authority, parts.path, "", ""))


def _refuse_masking_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Headers a caller may add; never one that stands in for the user's token.

    An Authorization header would be sent in place of the caller's own, so the
    service would answer as whoever the header names. The rule that one token
    is never swapped for another only holds if the header cannot be written.
    """
    for name in headers:
        if name.lower() == "authorization":
            raise ExternalServiceError(
                f"header {name} would stand in for the caller's token; "
                "a connection is read with the caller's own credential"
            )
    return dict(headers)


def _refuse_userinfo(url: str) -> None:
    """No credentials in the URL: httpx would send them as Basic auth in
    place of the caller's token, and allows() never sees them."""
    parts = urllib.parse.urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise ExternalServiceError(
            f"{_loggable(url)} carries credentials in its URL; "
            "the caller's token is the only credential sent"
        )


def _check_redirect(connection: Connection, url: str) -> None:
    """A hop the download may follow: https, no credentials, and a host the
    connection declares - loopback, the metadata service, or any of their
    spellings is just an undeclared host."""
    _refuse_userinfo(url)
    if not connection.allows_redirect(url):
        raise ExternalServiceError(
            f"redirected to {_loggable(url)}, which is not a declared "
            f"{connection.display_name} redirect host; the download stops here"
        )


async def _read_limited(response: httpx.Response, max_bytes: int) -> bytes:
    """Read at most max_bytes, aborting the stream instead of buffering more."""
    body = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ExternalServiceError(
                    f"the response is too large (over {max_bytes} bytes)"
                )
    finally:
        await response.aclose()
    return bytes(body)


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
        headers: Mapping[str, str] | None = None,
    ) -> None:
        # An id is resolved at request time against the running call's
        # registry; shared_client() is created before any call exists.
        self._target = target
        # Constants the service wants on every request, such as the API
        # version it speaks. Held by the client so a tool cannot forget one on
        # the call that needed it.
        self._headers = _refuse_masking_headers(headers or {})
        self._owns_client = client is None
        # One client serves every user of the connection, so a cookie stored
        # from one user's response would ride on the next user's request.
        # The jar's policy refuses every domain, so nothing is ever stored.
        self._client = client or httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT_SECONDS,
            cookies=http.cookiejar.CookieJar(
                policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
            ),
        )
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
        self,
        url: str,
        params: dict[str, Any] | None = None,
        max_bytes: int = MAX_FILE_BYTES,
        state: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """GET and return the JSON body after the policies' redaction."""
        response = await self._request(
            "GET", url, params=params, headers=headers, state=state
        )
        return await self._json(response, max_bytes)

    async def post_json(
        self,
        url: str,
        body: Any = None,
        params: dict[str, Any] | None = None,
        max_bytes: int = MAX_FILE_BYTES,
        state: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """POST a JSON body and return the JSON answer after redaction.

        For endpoints that read but take a POST to say what to read; search is
        usually one. Nothing here can tell such an endpoint from one that
        changes something, so a request that may already have been applied is
        not sent a second time.
        """
        response = await self._request(
            "POST", url, params=params, body=body, headers=headers, state=state
        )
        return await self._json(response, max_bytes)

    async def put_json(
        self,
        url: str,
        body: Any = None,
        params: dict[str, Any] | None = None,
        max_bytes: int = MAX_FILE_BYTES,
        state: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """PUT a JSON body and return the JSON answer after redaction.

        Updates are commonly PUT; a declared write tool is no use if its verb
        cannot be sent. Like POST, a request that may already have been
        applied is never sent a second time.
        """
        response = await self._request(
            "PUT", url, params=params, body=body, headers=headers, state=state
        )
        return await self._json(response, max_bytes)

    async def patch_json(
        self,
        url: str,
        body: Any = None,
        params: dict[str, Any] | None = None,
        max_bytes: int = MAX_FILE_BYTES,
        state: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """PATCH a JSON body and return the JSON answer after redaction.

        Same rules as put_json: the destination check, redaction, and the
        refusal to resend all apply.
        """
        response = await self._request(
            "PATCH", url, params=params, body=body, headers=headers, state=state
        )
        return await self._json(response, max_bytes)

    async def delete_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        max_bytes: int = MAX_FILE_BYTES,
        state: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """DELETE and return the JSON answer after redaction; 204 becomes None.

        No body: DELETE gives one no meaning (RFC 9110), and bulk endpoints
        say what to delete in the query. What a delete removes usually cannot
        be brought back, so like every other change it is never sent a second
        time once it may have been applied.
        """
        response = await self._request(
            "DELETE", url, params=params, headers=headers, state=state
        )
        return await self._json(response, max_bytes)

    async def _json(self, response: httpx.Response, max_bytes: int) -> Any:
        payload = await _read_limited(response, max_bytes)
        if not payload:
            # 204 No Content is how many services answer an update that
            # worked; reading it as a JSON failure would report the success
            # as an error.
            return None
        call = current_tool_call()
        rules = (
            call.redact_rules
            if call is not None and call.redact_rules
            else RedactRules()
        )
        return redact(json.loads(payload), rules)

    async def get_bytes(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        max_bytes: int = MAX_FILE_BYTES,
        state: Any = None,
    ) -> bytes:
        """GET a file, following each redirect only after checking it.

        An allowed endpoint may hand the download to another host, so
        redirects are followed - but every hop must be https, carry no
        credentials, and name a host, and the token only travels to the
        connection's own hosts.
        """
        connection = self._connection()
        response = await self._request("GET", url, params=params, state=state)
        for _ in range(MAX_REDIRECTS):
            if not response.is_redirect:
                break
            target = str(response.request.url.join(response.headers["location"]))
            await response.aclose()
            _check_redirect(connection, target)
            headers = (
                {**self._headers, **self._authorization(connection, target, state)}
                if connection.allows(target)
                # Off the connection's own hosts nothing of ours travels: not
                # the token, and not the constants that name this service.
                else {}
            )
            request = self._client.build_request("GET", target, headers=headers)
            response = await self._client.send(request, stream=True)
        else:
            await response.aclose()
            raise ExternalServiceError(
                f"{connection.display_name} kept redirecting the download"
            )
        if response.status_code >= 400:
            await response.aclose()
            raise ExternalServiceError(f"the download answered {response.status_code}")
        return await _read_limited(response, max_bytes)

    def _connection(self) -> Connection:
        return resolve_connection(self._target)

    def _authorization(
        self, connection: Connection, url: str, state: Any
    ) -> dict[str, str]:
        token = caller_token(connection, state)
        if token is None:
            # The user sees a re-authorization prompt; operators would not.
            logger.warning(
                "not reading %s without the caller's token url=%s",
                connection.id,
                _loggable(url),
            )
            raise ReauthorizationRequired(connection.reauthorization_message())
        return {"Authorization": f"Bearer {token}"}

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        state: Any = None,
    ) -> httpx.Response:
        connection = self._connection()
        _refuse_userinfo(url)
        if not connection.allows(url):
            # The message reaches the model, so the URL travels sanitized.
            raise ExternalServiceError(
                f"{_loggable(url)} is not a {connection.display_name} host; "
                "the token stays here"
            )
        # Header names do not care about case, so neither may the merge: a
        # dict would keep "Accept" next to "accept" and send both. The token
        # is put in last, so nothing can displace it.
        sent = httpx.Headers(self._headers)
        sent.update(_refuse_masking_headers(headers or {}))
        sent.update(self._authorization(connection, url, state))
        # A GET can be sent again because sending it again changes nothing.
        # Anything else may already have been applied by the time the answer
        # went missing, so only a refusal the service made before acting - a
        # rate limit - is worth a second attempt.
        idempotent = method == "GET"
        # Who read what is the service's audit log's business. The token is
        # the user's credential and the query is the user's work; neither is
        # logged.
        logger.info("reading %s url=%s", connection.id, _loggable(url))

        for attempt in range(MAX_ATTEMPTS):
            try:
                # Streamed, so a limit can stop a body instead of buffering it.
                request = self._client.build_request(
                    method, url, params=params, json=body, headers=sent
                )
                response = await self._client.send(request, stream=True)
            except httpx.HTTPError as error:
                if not idempotent or attempt == MAX_ATTEMPTS - 1:
                    raise ExternalServiceError(
                        f"could not connect to {connection.display_name}: {error}"
                    ) from error
                await asyncio.sleep(self._backoff)
                continue

            if response.status_code == 401:
                await response.aclose()
                # There is no way to refresh; authorization is Gemini Enterprise's job.
                logger.warning(
                    "token for %s was rejected url=%s", connection.id, _loggable(url)
                )
                # Not the reauthorization prompt. A token was forwarded, so
                # Gemini Enterprise holds a credential and shows no consent
                # screen; the user would be sent to approve and find nothing.
                raise AuthorizationRefused(connection.rejected_message())
            if response.status_code == 429:
                await response.aclose()
                if attempt == MAX_ATTEMPTS - 1:
                    self._log_refusal(connection, response, url)
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
                await response.aclose()
                if not idempotent or attempt == MAX_ATTEMPTS - 1:
                    self._log_refusal(connection, response, url)
                    raise ExternalServiceError(
                        f"{connection.display_name} answered {response.status_code}"
                    )
                await asyncio.sleep(self._backoff)
                continue
            if response.status_code >= 400:
                await response.aclose()
                # The message reaches the model without redaction; the status
                # is diagnosis enough, the body is the service's to keep.
                self._log_refusal(connection, response, url)
                raise ExternalServiceError(
                    f"{connection.display_name} answered {response.status_code}"
                )
            return response
        raise ExternalServiceError(f"{connection.display_name}: too many attempts")

    @staticmethod
    def _log_refusal(
        connection: Connection, response: httpx.Response, url: str
    ) -> None:
        """The status an answer was given up on, for the operator.

        The model is told the same status, but the model's answer is not the
        operator's log. The body stays out: it is the service's, and it may
        say anything about the user's data.
        """
        logger.warning(
            "%s answered %s url=%s",
            connection.id,
            response.status_code,
            _loggable(url),
        )
