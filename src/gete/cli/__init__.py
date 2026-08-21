"""The gete command."""

import sys
from pathlib import Path

import click

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
