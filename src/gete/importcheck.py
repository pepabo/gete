"""Loading the archive the way Agent Engine will, in a venv with only requirements.txt.

The developer's venv has every workspace dependency installed, so an import
that works there says nothing about the deployment. Agent Engine fails with
"created but does not start" and the reason is only in the container log.
This check fails first, with the traceback.
"""

import io
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gete.archive import REQUIREMENTS_FILE, build_archive
from gete.declaration import find_project_file, load_project

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

DEFAULT_PYTHON_VERSION = "3.12"

# Runs with the venv's python, the extracted archive first on sys.path.
# vertexai.init comes first: AdkApp looks up the project on import, and
# without one it goes looking for credentials and fails for the wrong reason.
PROBE = """\
import importlib
import sys

sys.path.insert(0, sys.argv[1])

import vertexai

vertexai.init(project="source-check-only", location=sys.argv[2])

entry = importlib.import_module("gete_entry")
app = getattr(entry, "app", None)
if app is None:
    raise SystemExit("gete_entry has no attribute named app")
methods = (
    "query", "async_query", "stream_query", "bidi_stream_query", "async_stream_query"
)
if not any(hasattr(app, method) for method in methods):
    kind = type(app).__name__
    raise SystemExit(f"app ({kind}) has none of {methods}; is it an AdkApp?")

# Telemetry initialization needs this at start-up.
import google.cloud.aiplatform.utils.resource_manager_utils  # noqa: E402, F401
print("ok")
"""


@dataclass(frozen=True)
class ImportCheckResult:
    ok: bool
    output: str


def import_check(
    directory: Path,
    *,
    runner: Runner = subprocess.run,
    gete_source: Path | None = None,
    uv: str = "uv",
) -> ImportCheckResult:
    """Pack the agent, install its requirements in a fresh venv, import the entry point.

    gete_source replaces the pinned gete line with a local checkout, for use
    before the pinned version exists on PyPI.
    """
    project = load_project(find_project_file(directory))
    result = build_archive(directory, project=project)
    agent = next(
        a for a in project.agents if a.directory.resolve() == directory.resolve()
    )
    runtime: dict[str, Any] = agent.data.get("runtime", {}).get("agent_engine", {})
    python_version = str(runtime.get("python_version", DEFAULT_PYTHON_VERSION))
    location = str(project.data["location"])

    with tempfile.TemporaryDirectory(prefix="gete-import-check-") as temp:
        root = Path(temp) / "archive"
        root.mkdir()
        with tarfile.open(fileobj=io.BytesIO(result.archive), mode="r:gz") as tar:
            tar.extractall(root, filter="data")
        requirements = root / REQUIREMENTS_FILE
        if gete_source is not None:
            lines = requirements.read_text(encoding="utf-8").splitlines()
            lines = [
                str(gete_source.resolve()) if line.startswith("gete==") else line
                for line in lines
            ]
            requirements.write_text("\n".join(lines) + "\n", encoding="utf-8")
        venv = Path(temp) / "venv"
        python = venv / ("Scripts" if sys.platform == "win32" else "bin") / "python"
        steps = [
            [uv, "venv", "--python", python_version, str(venv)],
            [uv, "pip", "install", "--python", str(python), "-r", str(requirements)],
            [str(python), "-c", PROBE, str(root), location],
        ]
        transcript: list[str] = []
        for argv in steps:
            completed = runner(argv, capture_output=True, text=True, check=False)
            transcript.append(f"$ {' '.join(argv[:3])} ...")
            if completed.returncode != 0:
                # All of it. The last line alone rarely says where the import died.
                transcript.append(completed.stdout)
                transcript.append(completed.stderr)
                return ImportCheckResult(ok=False, output="\n".join(transcript))
        transcript.append("ok")
        return ImportCheckResult(ok=True, output="\n".join(transcript))
