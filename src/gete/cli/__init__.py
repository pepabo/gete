"""The gete command."""

import sys
from pathlib import Path
from typing import Any

import click

from gete.archive import build_archive, external_program
from gete.declaration import find_project_file, load_project
from gete.errors import GeteError
from gete.register import NOTICE_FILE, register_project
from gete.terraform import check_generated, write_generated
from gete.validate import validate_project


@click.group()
@click.version_option(package_name="gete", prog_name="gete")
def main() -> None:
    """Declare agents in YAML, deploy them, register them with Gemini Enterprise."""


@main.command()
@click.option(
    "--check-secrets",
    is_flag=True,
    help="Also check that each secret the deployment reads has an enabled version.",
)
@click.option(
    "--import-check",
    is_flag=True,
    help="Also install each agent's requirements in a fresh venv and import it.",
)
def validate(check_secrets: bool, import_check: bool) -> None:
    """Check the declarations against their schemas and rules."""
    from gete.importcheck import import_check as run_import_check
    from gete.secrets import check_secrets as run_check_secrets

    try:
        project = load_project(find_project_file(Path.cwd()))
        problems = validate_project(project)
        if check_secrets and not problems:
            from gete.gcp import GcpClient

            problems.extend(
                run_check_secrets(
                    project, GcpClient(quota_project=str(project.data["project"]))
                )
            )
        for problem in problems:
            click.echo(str(problem))
        if problems:
            click.echo(f"{len(problems)} problem(s) found", err=True)
            sys.exit(1)
        if import_check:
            failed = 0
            for agent in project.agents:
                result = run_import_check(agent.directory)
                click.echo(
                    f"{agent.name}: import check {'passed' if result.ok else 'FAILED'}"
                )
                if not result.ok:
                    click.echo(result.output, err=True)
                    failed += 1
            if failed:
                sys.exit(1)
    except GeteError as error:
        click.echo(str(error), err=True)
        sys.exit(1)
    click.echo(f"OK: {len(project.agents)} agent(s) validated")


@main.command()
@click.argument(
    "directory",
    required=False,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--out", type=click.Path(dir_okay=False, path_type=Path), help="Where to write."
)
@click.option(
    "--external",
    is_flag=True,
    help='Speak Terraform\'s data "external" protocol: JSON on stdin, JSON on stdout.',
)
def archive(directory: Path | None, out: Path | None, external: bool) -> None:
    """Pack one agent directory into the archive Agent Engine receives."""
    if external:
        # Terraform's data "external" sends the directory in the stdin JSON
        # and passes no arguments.
        sys.exit(external_program(sys.stdin, sys.stdout, sys.stderr))
    if directory is None:
        raise click.UsageError("Missing argument 'DIRECTORY'.")
    try:
        result = build_archive(directory)
    except GeteError as error:
        click.echo(str(error), err=True)
        sys.exit(1)
    target = out or Path(f"{result.agent_name}.tar.gz")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(result.archive)
    click.echo(f"{target} sha256={result.sha256}")


@main.command()
@click.option(
    "--out",
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory for the generated files. Default: terraform/ next to gete.yaml.",
)
@click.option(
    "--check", is_flag=True, help="Compare instead of writing; exit 1 if out of date."
)
def terraform(out: Path | None, check: bool) -> None:
    """Write one Terraform module call per agent, or check that they are current."""
    try:
        project = load_project(find_project_file(Path.cwd()))
    except GeteError as error:
        click.echo(str(error), err=True)
        sys.exit(1)
    out_dir = out or project.root / "terraform"
    if check:
        differences = check_generated(project, out_dir)
        for difference in differences:
            click.echo(difference)
        if differences:
            sys.exit(1)
        click.echo(f"OK: {out_dir} is current")
        return
    for path in write_generated(project, out_dir):
        click.echo(str(path))


@main.command()
@click.argument("names", nargs=-1)
@click.option(
    "--notice",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path(NOTICE_FILE),
    show_default=True,
    help="Where to append the steps a person still has to take.",
)
def register(names: tuple[str, ...], notice: Path) -> None:
    """Create or update authorizations and bring registrations in line.

    Exits 0 when steps are left for a person (they are written to the notice
    file) and 1 only when an agent could not be processed at all.
    """
    from gete.gcp import GcpClient

    try:
        project = load_project(find_project_file(Path.cwd()))
        gcp = GcpClient(quota_project=str(project.data["project"]))
        summary = register_project(project, gcp, notice, list(names) or None)
    except GeteError as error:
        click.echo(str(error), err=True)
        sys.exit(1)
    for line in summary.messages:
        click.echo(line)
    if summary.needs_human:
        click.echo(f"steps for a person were written to {notice}")
    if summary.failed:
        click.echo(f"{len(summary.failed)} agent(s) could not be registered", err=True)
        sys.exit(1)


@main.command()
@click.argument("name")
def init(name: str) -> None:
    """Create agents/NAME from a template; create gete.yaml too if there is none."""
    from gete.scaffold import init_agent, init_project

    try:
        root = find_project_file(Path.cwd()).parent
        written: list[Path] = []
    except GeteError:
        root = Path.cwd()
        written = init_project(root)
    try:
        written.extend(init_agent(root, name))
    except GeteError as error:
        click.echo(str(error), err=True)
        sys.exit(1)
    for path in written:
        click.echo(str(path))
    if not written:
        click.echo("nothing to do; every file already exists")


@main.command()
@click.argument("connection_id", required=False)
def connections(connection_id: str | None) -> None:
    """List the connections agents can declare, or describe one of them.

    With an id, print what a person has to prepare before anyone can
    authorize it: the OAuth client's secret names, the redirect URI, and
    whatever the connection declares under setup.
    """
    from gete.connection import Registry
    from gete.connections_listing import (
        connections_table,
        format_connection,
        format_table,
    )

    try:
        project = load_project(find_project_file(Path.cwd()))
        registry = Registry.from_catalog(project.connection_overrides)
    except GeteError:
        registry = Registry.from_catalog()
    if connection_id is None:
        click.echo(format_table(connections_table(registry)), nl=False)
        return
    try:
        connection = registry.get(connection_id, include_retired=True)
    except GeteError as error:
        click.echo(str(error), err=True)
        sys.exit(1)
    click.echo(format_connection(connection), nl=False)


@main.command()
@click.argument("names", nargs=-1)
def graph(names: tuple[str, ...]) -> None:
    """Print a Mermaid diagram of the agents, their engines, tools, and connections."""
    from gete.graph import mermaid

    try:
        project = load_project(find_project_file(Path.cwd()))
    except GeteError as error:
        click.echo(str(error), err=True)
        sys.exit(1)
    click.echo(mermaid(project, list(names) or None), nl=False)


@main.command()
@click.argument("name")
def run(name: str) -> None:
    """Talk to an agent locally. Tokens come from GETE_TOKEN_<CONNECTION> variables."""
    import asyncio
    import os

    from gete.run import (
        build_local_agent,
        converse,
        find_agent,
        initial_state,
        missing_tokens,
    )

    try:
        project = load_project(find_project_file(Path.cwd()))
        agent = build_local_agent(project, name)
        declared = find_agent(project, name)
    except GeteError as error:
        click.echo(str(error), err=True)
        sys.exit(1)
    state = initial_state(name, declared.connections, os.environ)
    missing = missing_tokens(name, declared.connections, state)
    if missing:
        click.echo(
            f"no token for {', '.join(missing)}; set GETE_TOKEN_<CONNECTION>", err=True
        )

    def prompts() -> Any:
        while True:
            try:
                line = click.prompt("you", prompt_suffix="> ")
            except (EOFError, click.Abort):
                return
            if line.strip() in ("exit", "quit"):
                return
            yield line

    asyncio.run(
        converse(agent, state, prompts(), lambda text: click.echo(f"{name}> {text}"))
    )
