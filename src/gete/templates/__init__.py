"""Files gete writes verbatim into archives and generated directories."""

from importlib.resources import files


def template_text(name: str) -> str:
    return files("gete.templates").joinpath(name).read_text(encoding="utf-8")
