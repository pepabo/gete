"""gete: declare agents in YAML, deploy them, register them with Gemini Enterprise."""

from importlib.metadata import PackageNotFoundError, version


def _version() -> str:
    try:
        return version("gete")
    except PackageNotFoundError:
        # Vendored inside an archive: the source travels without dist
        # metadata. The version that matters there is recorded in
        # agent.resolved.yaml at pack time.
        return "0.0.0+vendored"


# Mirror the version from package metadata, which is derived from the git
# tag. A literal here would drift from the tag and ship under the wrong version.
__version__ = _version()
