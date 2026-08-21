"""Entry point Agent Engine imports. gete writes it into every archive."""

from pathlib import Path

from gete.runtime import app as _app

app = _app(Path(__file__).with_name("agent.resolved.yaml"))
