"""gete: declare agents in YAML, deploy them, register them with Gemini Enterprise."""

from importlib.metadata import version

# Mirror the version from package metadata, which is derived from the git
# tag. A literal here would drift from the tag and ship under the wrong version.
__version__ = version("gete")
