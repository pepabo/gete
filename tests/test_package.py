"""パッケージの器。版の持ち方を縛る。"""

import importlib.metadata
import tomllib
from pathlib import Path

import gete

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_版はパッケージのメタデータと同じ() -> None:
    """gete.__version__ はタグから作ったメタデータを写す。別の場所で持たない。"""
    assert gete.__version__ == importlib.metadata.version("gete")


def test_版は_pyproject_に静的に書かない() -> None:
    """版はタグだけで持つ。pyproject.toml に書くと tagpr の書き換えと二重になる。"""
    project = tomllib.loads(PYPROJECT.read_text())["project"]
    assert "version" not in project
    assert "version" in project["dynamic"]
