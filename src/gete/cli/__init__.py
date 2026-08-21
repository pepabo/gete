"""The gete command."""

import sys
from pathlib import Path

import click

from gete.archive import build_archive, external_program
from gete.declaration import find_project_file, load_project
from gete.errors import GeteError
from gete.validate import validate_project


@click.group()
@click.version_option(package_name="gete", prog_name="gete")
def main() -> None:
    """Declare agents in YAML, deploy them, register them with Gemini Enterprise."""


@main.command()
def validate() -> None:
    """Check the declarations against their schemas and rules."""
    try:
        project = load_project(find_project_file(Path.cwd()))
    except GeteError as error:
        click.echo(str(error), err=True)
        sys.exit(1)
    problems = validate_project(project)
    for problem in problems:
        click.echo(str(problem))
    if problems:
        click.echo(f"{len(problems)} problem(s) found", err=True)
        sys.exit(1)
    click.echo(f"OK: {len(project.agents)} agent(s) validated")


@main.command()
@click.argument(
    "directory", type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option(
    "--out", type=click.Path(dir_okay=False, path_type=Path), help="Where to write."
)
@click.option(
    "--external",
    is_flag=True,
    help='Speak Terraform\'s data "external" protocol: JSON on stdin, JSON on stdout.',
)
def archive(directory: Path, out: Path | None, external: bool) -> None:
    """Pack one agent directory into the archive Agent Engine receives."""
    if external:
        sys.exit(external_program(sys.stdin, sys.stdout, sys.stderr))
    try:
        result = build_archive(directory)
    except GeteError as error:
        click.echo(str(error), err=True)
        sys.exit(1)
    target = out or Path(f"{result.agent_name}.tar.gz")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(result.archive)
    click.echo(f"{target} sha256={result.sha256}")
