"""The gete command."""

import sys
from pathlib import Path

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
@click.option(
    "--gete-source",
    type=click.Path(exists=True, path_type=Path),
    help="With --import-check: install gete from this checkout instead of PyPI.",
)
def validate(check_secrets: bool, import_check: bool, gete_source: Path | None) -> None:
    """Check the declarations against their schemas and rules."""
    from gete.importcheck import import_check as run_import_check
    from gete.secrets import check_secrets as run_check_secrets

    if gete_source is not None and not import_check:
        click.echo("--gete-source only has an effect with --import-check", err=True)
        sys.exit(1)
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
                result = run_import_check(agent.directory, gete_source=gete_source)
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
