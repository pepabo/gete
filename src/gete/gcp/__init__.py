"""REST access to Google Cloud with Application Default Credentials.

Everything gete does against GCP goes through one client: token, quota
project header, JSON, error shape, paging, and long-running operations.
"""

import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from gete.errors import GeteError

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
DEFAULT_TIMEOUT_SECONDS = 60.0
OPERATION_POLL_SECONDS = 5.0
ERROR_BODY_CHARS = 400


class GcpError(GeteError):
    """A GCP API answered with an error, or an operation failed."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}" if status else message)
        self.status = status
        self.message = message


class GcpApi(Protocol):
    """What the commands need from a client; the tests provide a fake."""

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any: ...
    def post(
        self, url: str, body: Any, params: dict[str, Any] | None = None
    ) -> Any: ...
    def patch(
        self, url: str, body: Any, params: dict[str, Any] | None = None
    ) -> Any: ...
    def delete(self, url: str, params: dict[str, Any] | None = None) -> Any: ...
    def list_all(
        self, url: str, key: str, params: dict[str, Any] | None = None
    ) -> list[Any]: ...


def adc_token_provider() -> Callable[[], str]:
    """Tokens from Application Default Credentials, refreshed when expired."""
    import google.auth
    from google.auth.transport.requests import Request

    credentials: Any = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])[0]

    def token() -> str:
        if not credentials.valid:
            credentials.refresh(Request())
        return str(credentials.token)

    return token


class GcpClient:
    """httpx-based client that speaks JSON to Google APIs."""

    def __init__(
        self,
        quota_project: str,
        token_provider: Callable[[], str] | None = None,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._quota_project = quota_project
        self._token = token_provider or adc_token_provider()
        self._http = http or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)
        self._sleep = sleep

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", url, None, params)

    def post(self, url: str, body: Any, params: dict[str, Any] | None = None) -> Any:
        return self._request("POST", url, body, params)

    def patch(self, url: str, body: Any, params: dict[str, Any] | None = None) -> Any:
        return self._request("PATCH", url, body, params)

    def delete(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("DELETE", url, None, params)

    def list_all(
        self, url: str, key: str, params: dict[str, Any] | None = None
    ) -> list[Any]:
        """Follow nextPageToken until the end and return every item under key."""
        items: list[Any] = []
        page_token: str | None = None
        while True:
            page_params = dict(params or {})
            if page_token:
                page_params["pageToken"] = page_token
            page = self.get(url, page_params)
            items.extend(page.get(key, []))
            page_token = page.get("nextPageToken")
            if not page_token:
                return items

    def wait_operation(self, base_url: str, operation: dict[str, Any]) -> Any:
        """Poll a long-running operation until done; return its response."""
        name = operation["name"]
        current = operation
        while not current.get("done"):
            self._sleep(OPERATION_POLL_SECONDS)
            current = self.get(f"{base_url}/{name}")
        if "error" in current:
            error = current["error"]
            raise GcpError(int(error.get("code", 0)), str(error.get("message", error)))
        return current.get("response", {})

    def _request(
        self, method: str, url: str, body: Any, params: dict[str, Any] | None
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
            # Some APIs insist on a quota project even when the credential has one.
            "X-Goog-User-Project": self._quota_project,
        }
        response = self._http.request(
            method, url, json=body, params=params, headers=headers
        )
        if response.status_code >= 400:
            raise GcpError(response.status_code, _error_message(response))
        if not response.content:
            return {}
        return response.json()


def _error_message(response: httpx.Response) -> str:
    try:
        error = response.json().get("error", {})
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:ERROR_BODY_CHARS]
    except ValueError:
        pass
    return response.text[:ERROR_BODY_CHARS]
