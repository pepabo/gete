# gete

Write an `agent.yaml`, and the agent is deployed to Agent Runtime
(Vertex AI Agent Engine), registered with Gemini Enterprise, and wired to
per-user authorizations.

People adding an agent do not write Python. Instructions live in Markdown;
connections and tools are declared in YAML. Python is only needed for tools
whose logic you implement yourself.

The name is "gate", Gemini flavoured: the gate agents walk through into
Gemini Enterprise.

## Status

Early development. Nothing can be deployed yet.

## Development

```sh
uv sync --all-extras
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

The version is derived from git tags (`hatch-vcs`). It is not written in
`pyproject.toml`.

## License

Apache-2.0
