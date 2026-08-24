"""validate --import-check: load the archive the way Agent Engine would."""

import subprocess
from pathlib import Path
from typing import Any

from conftest import ProjectBuilder

from gete.importcheck import PROBE, import_check


class Runner:
    """Stands in for subprocess.run; records commands and answers as told."""

    def __init__(self, fail_at: int | None = None, stderr: str = "") -> None:
        self.commands: list[list[str]] = []
        self.seen_files: list[set[str]] = []
        self.fail_at = fail_at
        self.stderr = stderr

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(argv))
        # What the extracted archive held at the time of the call.
        for arg in argv:
            if arg.endswith("requirements.txt"):
                self.seen_files.append({p.name for p in Path(arg).parent.iterdir()})
        code = 1 if self.fail_at == len(self.commands) else 0
        return subprocess.CompletedProcess(
            argv, code, stdout="", stderr=self.stderr if code else ""
        )


def agent_dir(project: ProjectBuilder) -> Path:
    project.write_project(
        {"version": 1, "project": "example-project", "location": "us-central1"}
    )
    return project.write_agent("mail-triage")


def test_venv_install_and_probe_run_in_that_order(project: ProjectBuilder) -> None:
    runner = Runner()
    result = import_check(agent_dir(project), runner=runner)
    assert result.ok, result.output
    venv, install, probe = runner.commands
    assert venv[:3] == ["uv", "venv", "--python"] and venv[3] == "3.12"
    assert install[:3] == ["uv", "pip", "install"]
    assert "--python" in install and install[-2] == "-r"
    assert install[-1].endswith("requirements.txt")
    assert probe[1:3] == ["-c", PROBE]
    assert probe[0].startswith(venv[-1]), "the probe runs with the venv's python"


def test_the_archive_is_extracted_next_to_the_requirements(
    project: ProjectBuilder,
) -> None:
    runner = Runner()
    import_check(agent_dir(project), runner=runner)
    assert {
        "gete_entry.py",
        "agent.resolved.yaml",
        "requirements.txt",
    } <= runner.seen_files[0]


def test_the_extracted_archive_carries_gete_so_no_local_install_is_needed(
    project: ProjectBuilder,
) -> None:
    """The vendored copy is what the probe imports; requirements hold only deps."""
    runner = Runner()
    import_check(agent_dir(project), runner=runner)
    assert any(name == "gete" for name in runner.seen_files[0])


def test_failure_carries_the_whole_stderr(project: ProjectBuilder) -> None:
    """The last line alone rarely says where the import died."""
    stderr = (
        "Traceback (most recent call last):\n  File x\n  File y\n"
        "ModuleNotFoundError: No module named 'pandas'\n"
    )
    result = import_check(agent_dir(project), runner=Runner(fail_at=3, stderr=stderr))
    assert not result.ok
    assert "Traceback" in result.output and "pandas" in result.output


def test_probe_checks_what_agent_engine_needs() -> None:
    """Init Vertex AI, import the entry point, find a query method, load telemetry."""
    assert "vertexai.init(" in PROBE
    assert "source-check-only" in PROBE
    assert 'import_module("gete_entry")' in PROBE
    for method in (
        "query",
        "async_query",
        "stream_query",
        "bidi_stream_query",
        "async_stream_query",
    ):
        assert method in PROBE
    assert "resource_manager_utils" in PROBE
    assert PROBE.index("vertexai.init(") < PROBE.index('import_module("gete_entry")')


def test_python_version_comes_from_the_declaration(project: ProjectBuilder) -> None:
    project.write_project(
        {"version": 1, "project": "example-project", "location": "us-central1"}
    )
    directory = project.write_agent(
        "mail-triage", {"runtime": {"agent_engine": {"python_version": "3.13"}}}
    )
    runner = Runner()
    import_check(directory, runner=runner)
    assert runner.commands[0][3] == "3.13"
