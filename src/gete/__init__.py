"""gete: YAML の宣言から Agent Runtime へ載せ、Gemini Enterprise に登録する。"""

from importlib.metadata import version

# 版はタグから導いたメタデータを写すだけ。ここに文字列を書くと
# タグとずれたまま配布される
__version__ = version("gete")
