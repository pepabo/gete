# gete

`agent.yaml` を書くと、Agent Runtime（Vertex AI Agent Engine）に載り、
Gemini Enterprise に登録され、利用者ごとの認可が紐づく。

エージェントを足す人は Python を書かない。指示は Markdown、連携先と
ツールは YAML に書く。Python を書くのは、自分で処理を書いたツールが
要るときだけ。

名前は gate の Gemini 版。エージェントが Gemini Enterprise へ入っていく門。

## 状態

作り始めたところ。まだ何も載せられない。

## 開発

```sh
uv sync
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

版はタグから導く（`hatch-vcs`）。`pyproject.toml` には書かない。

## ライセンス

Apache-2.0
