"""Builders for declaration trees on disk, shared by the validate and CLI tests."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from gete.gcp import GcpError

MINIMAL_AGENT: dict[str, Any] = {
    "name": "mail-triage",
    "display_name": "Mail triage",
    "description": "Sorts pasted mail by urgency",
    "model": "gemini-2.5-flash",
    "instruction": "./instruction.md",
}


class ProjectBuilder:
    """Writes a gete.yaml and agents under a temporary root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.agents_dir = root / "agents"
        self.agents_dir.mkdir()
        self.write_project(
            {"version": 1, "project": "example-project", "location": "us-central1"}
        )

    def write_project(self, document: dict[str, Any]) -> Path:
        path = self.root / "gete.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False))
        return path

    def write_agent(
        self,
        directory: str,
        document: dict[str, Any] | None = None,
        *,
        instruction: str | None = "You sort mail.",
    ) -> Path:
        agent_dir = self.agents_dir / directory
        agent_dir.mkdir(parents=True, exist_ok=True)
        body = {**MINIMAL_AGENT, "name": directory, **(document or {})}
        (agent_dir / "agent.yaml").write_text(yaml.safe_dump(body, sort_keys=False))
        if instruction is not None:
            (agent_dir / "instruction.md").write_text(instruction)
        return agent_dir

    def write_policies(self, name: str, document: list[dict[str, Any]]) -> Path:
        path = self.root / "policies" / f"{name}.yaml"
        path.parent.mkdir(exist_ok=True)
        path.write_text(yaml.safe_dump(document, sort_keys=False))
        return path


@pytest.fixture
def project(tmp_path: Path) -> ProjectBuilder:
    return ProjectBuilder(tmp_path)


class FakeGcp:
    """Answers from a table of (method, url) and records every write."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Callable[[dict[str, Any] | None], Any]] = {}
        self.calls: list[
            tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]
        ] = []

    def route(self, method: str, url: str, response: Any) -> None:
        self.routes[(method, url)] = (
            response if callable(response) else (lambda body: response)
        )

    def _call(self, method: str, url: str, body: Any, params: Any) -> Any:
        self.calls.append((method, url, params, body))
        try:
            handler = self.routes[(method, url)]
        except KeyError:
            raise GcpError(404, f"{method} {url} not routed") from None
        result = handler(body)
        if isinstance(result, Exception):
            raise result
        return result

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self._call("GET", url, None, params)

    def post(self, url: str, body: Any, params: dict[str, Any] | None = None) -> Any:
        return self._call("POST", url, body, params)

    def patch(self, url: str, body: Any, params: dict[str, Any] | None = None) -> Any:
        return self._call("PATCH", url, body, params)

    def delete(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self._call("DELETE", url, None, params)

    def list_all(
        self, url: str, key: str, params: dict[str, Any] | None = None
    ) -> list[Any]:
        """Follows nextPageToken, so a paging bug shows up here and not only live."""
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

    def writes(
        self, method: str
    ) -> list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]]:
        return [
            (url, params, body) for m, url, params, body in self.calls if m == method
        ]
